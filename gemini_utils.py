import os
import json
import logging
from google import genai
from google.genai import types
import google.auth
from google.api_core import exceptions as google_exceptions
import datetime
import re
from colorama import Fore, Style, init
import base64 

init(autoreset=True)

GENERATION_LOG_FILE = "generation_log.txt"
BASE_GEMENI_MODEL = os.getenv("DEFAULT_GEMINI_MODEL", "gemini-2.0-flash-001")

gemini_client = None

def init_gemini_client():
    """Инициализирует клиент Gemini API."""
    global gemini_client, BASE_GEMENI_MODEL
    logging.info("Инициализация клиента Gemini...")
    try:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
             logging.info("GOOGLE_API_KEY не найден, попытка использовать Application Default Credentials (ADC)...")
             try:
                 credentials, project_id = google.auth.default()
                 gemini_client = genai.Client() 
                 logging.info(f"Используются ADC. Project ID: {project_id}")
             except google.auth.exceptions.DefaultCredentialsError:
                 raise ValueError("GOOGLE_API_KEY не установлен и Application Default Credentials не найдены.")
        else:
             logging.info("Используется GOOGLE_API_KEY из переменных окружения.")
             gemini_client = genai.Client(api_key=api_key) 

        gemini_client.models.list()
        logging.info(Fore.GREEN + "Клиент Gemini создан и аутентифицирован.")

        try:
             full_model_name = f'{BASE_GEMENI_MODEL}'
             gemini_client.models.get(model=full_model_name)
             logging.info(Fore.GREEN + f"Базовая модель '{BASE_GEMENI_MODEL}' (проверена как '{full_model_name}') доступна.")
        except Exception as model_err:
             logging.warning(Fore.YELLOW + f"Предупреждение: Не удалось проверить доступность модели '{BASE_GEMENI_MODEL}': {model_err}")
             logging.warning(Fore.YELLOW + f"Убедитесь, что модель '{BASE_GEMENI_MODEL}' существует и доступна вашему ключу/аккаунту.")

        return gemini_client 

    except ValueError as e:
        logging.error(Fore.RED + f"Ошибка инициализации Gemini: {e}")
        gemini_client = None
        return None
    except Exception as e:
        logging.error(Fore.RED + f"Неожиданная ошибка при инициализации Gemini: {e}", exc_info=True)
        gemini_client = None
        return None

def generate_chat_reply_original(model_name, system_prompt, chat_history, config=None):
    """
    Генерирует ответ на основе истории чата Telegram, используя логику
    из предоставленной функции generate_tuned_comment и твои последние исправления.

    Args:
        model_name (str): Имя модели Gemini (e.g., 'gemini-2.0-flash-001' or 'tunedModels/your-model-id').
        system_prompt (str | None): Системная инструкция.
        chat_history (list): Список сообщений из Telegram в формате [{'role': 'user'/'model', 'parts': [{'text': ...}]}].
        config (dict | None): Дополнительные параметры генерации (temperature, top_p, etc.).

    Returns:
        tuple: (generated_text: str | None, error_message: str | None)
    """
    global GENERATION_LOG_FILE, gemini_client, BASE_GEMENI_MODEL

    if not gemini_client:
        logging.error("Клиент Gemini не инициализирован.")
        return None, "Клиент Gemini не инициализирован."
    if not chat_history:
        logging.warning("История чата пуста. Нечего отправлять модели.")
        return None, "История чата пуста."

    history_for_api = list(chat_history)

    if history_for_api and history_for_api[-1].get('role') == 'model':
        logging.info(Fore.CYAN + "Последнее сообщение от 'model'. Добавляем фиктивное сообщение 'user' для запроса к API.")
        dummy_user_message = {
            "role": "user",
            "parts": [{"text": "[собеседник молчит]"}]
        }
        history_for_api.append(dummy_user_message)
    chat_history = history_for_api
    original_model_name_input = model_name
    if not model_name:
        model_name = BASE_GEMENI_MODEL
        logging.info(f"Имя модели не указано, используется по умолчанию: {model_name}")
    is_tuned_model = model_name.startswith("tunedModels/")
    logging.info(f"Используемое имя модели для API: {model_name} (Тюнингованная: {is_tuned_model})")

    contents_list = []
    try:
        for msg_data in chat_history:
            role = msg_data.get('role')
            parts_data = msg_data.get('parts')
            if not (role and isinstance(parts_data, list) and parts_data):
                logging.warning(f"Пропущено некорректное сообщение в истории (нет роли или parts): {msg_data}")
                continue

            api_parts = []
            for part_item in parts_data:
                # Сначала текст
                if 'text' in part_item and part_item.get('text'):
                    api_parts.append(types.Part.from_text(text=part_item['text']))
                
                elif 'video_base64' in part_item and 'mime_type' in part_item:
                    try:
                        video_data = base64.b64decode(part_item['video_base64'])
                        api_parts.append(types.Part.from_bytes(
                            mime_type=part_item['mime_type'], data=video_data
                        ))
                        logging.info(Fore.BLUE + "Добавлен Part.from_bytes (ВИДЕО) в запрос к API.")
                    except Exception as e:
                        logging.error(f"Ошибка декодирования Base64 для видео: {e}")
                        api_parts.append(types.Part.from_text(text="[Ошибка: не удалось обработать видео]"))

                elif 'audio_base64' in part_item and part_item['mime_type'] in ["audio/mpeg", "audio/ogg"]:
                    try:
                        audio_data = base64.b64decode(part_item['audio_base64'])
                        api_parts.append(types.Part.from_bytes(
                            mime_type=part_item['mime_type'], data=audio_data
                        ))
                        logging.info(Fore.BLUE + f"Добавлен Part.from_bytes ({part_item['mime_type'].upper()}) в запрос к API.")
                    except Exception as e:
                        logging.error(f"Ошибка декодирования Base64 для аудио: {e}")
                        api_parts.append(types.Part.from_text(text="[Ошибка: не удалось обработать аудио]"))

                elif 'file_base64' in part_item and part_item['mime_type'] == "application/pdf":
                    try:
                        file_data = base64.b64decode(part_item['file_base64'])
                        api_parts.append(types.Part.from_bytes(
                            mime_type=part_item['mime_type'], data=file_data
                        ))
                        logging.info(Fore.BLUE + "Добавлен Part.from_bytes (PDF ФАЙЛ) в запрос к API.")
                    except Exception as e:
                        logging.error(f"Ошибка декодирования Base64 для файла: {e}")
                        api_parts.append(types.Part.from_text(text="[Ошибка: не удалось обработать PDF-файл]"))

                elif 'image_base64' in part_item and 'mime_type' in part_item:
                    try:
                        image_data = base64.b64decode(part_item['image_base64'])
                        api_parts.append(types.Part.from_bytes(
                            mime_type=part_item['mime_type'], data=image_data
                        ))
                        logging.info("Добавлен Part.from_bytes (КАРТИНКА) в запрос к API.")
                    except Exception as e:
                        logging.error(f"Ошибка декодирования Base64 для картинки: {e}")
                        api_parts.append(types.Part.from_text(text="[Ошибка: не удалось обработать изображение]"))
            
            if api_parts:
                contents_list.append(types.Content(role=role, parts=api_parts))
            else:
                logging.debug(f"Пропущено сообщение без валидных parts: {msg_data}")

        if not contents_list:
            logging.error("Не удалось сформировать contents_list из chat_history.")
            return None, "Ошибка обработки истории чата (пустой результат)."
    except Exception as e:
        logging.error(f"Ошибка при преобразовании chat_history в contents_list: {e}", exc_info=True)
        return None, f"Внутренняя ошибка при обработке истории: {e}"

    api_args = { "model": model_name, "contents": contents_list, }
    system_instruction_text_to_log = None
    generation_config_to_use = None
    base_gen_config_obj = None
    if isinstance(config, dict) and config:
        try:
            base_gen_config_obj = types.GenerationConfig(**config)
            logging.info(f"Используются базовые параметры генерации из config: {config}")
        except Exception as cfg_err:
            logging.warning(f"Не удалось создать GenerationConfig: {cfg_err}. Игнорируются.")
            base_gen_config_obj = types.GenerationConfig()
    elif isinstance(config, types.GenerationConfig):
        base_gen_config_obj = config
    else:
        base_gen_config_obj = types.GenerationConfig()
        logging.info("Используется конфигурация генерации по умолчанию.")
    if system_prompt:
        now = datetime.datetime.now()
        time_suffix = f"\n сегодня {now.strftime('%Y-%m-%d %H:%M:%S')}"
        system_prompt += time_suffix
        if is_tuned_model:
            logging.info(Fore.CYAN + "Модель тюнингованная: системный промпт добавляется в историю.")
            system_message = {"role": "user", "parts": [{"text": system_prompt}]}
            contents_list.append(types.Content(**system_message))
            api_args["contents"] = contents_list
            system_instruction_text_to_log = system_prompt
        else:
            logging.info(Fore.CYAN + "Модель базовая: системный промпт передается через GenerateContentConfig.")
            try:
                system_config = types.GenerateContentConfig(system_instruction=[types.Part.from_text(text=system_prompt)])
                generation_config_to_use = system_config
                system_instruction_text_to_log = system_prompt
            except Exception as sys_cfg_err:
                logging.error(f"Ошибка создания GenerateContentConfig: {sys_cfg_err}", exc_info=True)
                generation_config_to_use = base_gen_config_obj
                system_instruction_text_to_log = "[ОШИБКА СОЗДАНИЯ]"
    else:
        generation_config_to_use = base_gen_config_obj
        system_instruction_text_to_log = None
    if not is_tuned_model:
        if generation_config_to_use and ((hasattr(generation_config_to_use, 'temperature') and generation_config_to_use.temperature is not None) or (hasattr(generation_config_to_use, 'top_p') and generation_config_to_use.top_p is not None) or (hasattr(generation_config_to_use, 'top_k') and generation_config_to_use.top_k is not None) or (isinstance(generation_config_to_use, types.GenerateContentConfig) and hasattr(generation_config_to_use, 'system_instruction') and generation_config_to_use.system_instruction)):
            api_args["config"] = generation_config_to_use
    else:
        api_args.pop("config", None)
    try:
        log_prefix = f"ГЕНЕРАЦИЯ ответа (Модель: {model_name}) [История: {len(contents_list)}]"
        if system_instruction_text_to_log and not system_instruction_text_to_log.startswith("["): log_prefix += " [С system_instruction]"
        elif system_instruction_text_to_log: log_prefix += f" {system_instruction_text_to_log}"
        logging.info(Fore.MAGENTA + f"Отправка запроса на {log_prefix}...")
        try:
            with open(GENERATION_LOG_FILE, 'w', encoding='utf-8') as log_f:
                log_f.write(f"Model: {model_name}\nConfig: {repr(api_args.get('config', 'N/A'))}\n")
                if system_instruction_text_to_log: log_f.write("="*30 + " SYSTEM INSTRUCTION " + "="*30 + f"\n{system_instruction_text_to_log}\n")
                log_content_to_write = ""
                if isinstance(contents_list, list):
                    formatted_log_parts = []
                    for item in contents_list:
                        role_prefix = f"--- {item.role.upper()} ---"
                        text_part = "\n".join([p.text for p in item.parts if hasattr(p, 'text')])
                        image_part = "[IMAGE DATA PRESENT]" if any(hasattr(p.blob, 'data') for p in item.parts if hasattr(p, 'blob')) else ""
                        formatted_log_parts.append(f"{role_prefix}\n{text_part}\n{image_part}".strip())
                    log_content_to_write = "\n\n".join(formatted_log_parts)
                log_f.write("="*30 + " CONTENTS " + "="*30 + f"\n{log_content_to_write}\n\n--- RAW API ARGS ---\n{repr(api_args)}\n")
        except Exception as log_e: logging.warning(f"Не удалось записать лог: {log_e}")
        response = gemini_client.models.generate_content(**api_args)
        generated_comment = None; reason_empty = "Причина неизвестна"
        if not response.candidates:
            if hasattr(response, 'prompt_feedback') and response.prompt_feedback and getattr(response.prompt_feedback,'block_reason', None):
                block_reason = response.prompt_feedback.block_reason
                reason_name = getattr(block_reason, 'name', str(block_reason))
                reason_msg = getattr(response.prompt_feedback, 'block_reason_message', '')
                reason_empty = f"Заблокировано Gemini: {reason_msg or reason_name}"
            else: reason_empty = "Ответ не содержит кандидатов."
        elif response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
            generated_comment = "".join(part.text for part in response.candidates[0].content.parts if hasattr(part, 'text')).strip()
            if not generated_comment:
                reason_empty = "Текст ответа пустой."
                finish_reason = getattr(response.candidates[0], 'finish_reason', None)
                if finish_reason: reason_empty += f" Причина завершения: {getattr(finish_reason, 'name', str(finish_reason))}"
        else:
            reason_empty = "Структура ответа не содержит текст."
            finish_reason = getattr(response.candidates[0], 'finish_reason', None)
            if finish_reason: reason_empty += f" Причина завершения: {getattr(finish_reason, 'name', str(finish_reason))}"
        if generated_comment is None:
            error_msg = f"Модель '{model_name}' вернула пустой ответ. {reason_empty}"
            logging.warning(Fore.YELLOW + error_msg)
            return None, error_msg
        logging.info(Fore.GREEN + f"Ответ успешно сгенерирован '{model_name}'.")
        return generated_comment, None
    except google_exceptions.GoogleAPIError as e:
        error_message = f"Ошибка API Google при вызове '{model_name}': {e}"
        logging.error(Fore.RED + error_message)
        http_code = getattr(e, 'code', None) or getattr(e, 'resp', {}).get('status')
        if http_code:
            try: http_code = int(http_code)
            except: pass
            if http_code == 400: error_message += " (400 Bad Request: Проверьте формат данных в generation_log.txt)"
            elif http_code == 404: error_message += " (404 Not Found: Модель не найдена. Проверьте имя.)"
            elif http_code == 429: error_message += " (429 Resource Exhausted: Квоты API.)"
            elif http_code == 500: error_message += " (500 Internal Server Error: Ошибка сервера Gemini.)"
            elif http_code == 503: error_message += " (503 Service Unavailable: Сервис недоступен.)"
            elif http_code == 403: error_message += " (403 Forbidden: Ошибка авторизации/доступа.)"
            else: error_message += f" (HTTP статус: {http_code})"
        return None, error_message
    except Exception as e:
        logging.error(Fore.RED + f"Неожиданная ошибка при вызове модели '{model_name}': {e}", exc_info=True)
        error_message = str(e)
        response_obj_during_exception = locals().get('response')
        if response_obj_during_exception:
            logging.warning(Fore.YELLOW + f"Объект 'response' во время исключения: {response_obj_during_exception}")
            try:
                if (hasattr(response_obj_during_exception, 'prompt_feedback') and
                    response_obj_during_exception.prompt_feedback is not None and
                    getattr(response_obj_during_exception.prompt_feedback, 'block_reason', None)):
                    block_reason = response_obj_during_exception.prompt_feedback.block_reason
                    reason_name = getattr(block_reason, 'name', str(block_reason))
                    reason_msg = getattr(response_obj_during_exception.prompt_feedback, 'block_reason_message', '')
                    error_message = f"Заблокировано Gemini: {reason_msg or reason_name} (перехвачено в Exception)"
            except Exception as inner_e:
                logging.warning(Fore.YELLOW + f"Доп. ошибка при проверке prompt_feedback в Exception: {inner_e}")
        try:
            if hasattr(e, 'message') and e.message and error_message == str(e):
                error_message = e.message
        except Exception as inner_e:
            logging.warning(Fore.YELLOW + f"Дополнительная ошибка при извлечении e.message: {inner_e}")
        suffix = " (Проверьте логи и generation_log.txt)"
        if model_name in str(e) or model_name in error_message:
            if not error_message.endswith(suffix):
                error_message += suffix
        return None, f"Ошибка модели '{original_model_name_input}': {error_message}"