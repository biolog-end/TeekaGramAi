import asyncio
import re
import random
import string
import emoji
import os
import json  
from telethon import TelegramClient, errors, functions
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.types import (
    MessageMediaPhoto, MessageMediaDocument, MessageMediaUnsupported, MessageMediaContact,
    MessageMediaGeo, MessageMediaGame, MessageMediaInvoice, MessageMediaPoll,
    MessageMediaVenue,
    MessageService, DocumentAttributeVideo, DocumentAttributeAudio,
    InputDocument, SendMessageChooseStickerAction, ReactionEmoji,
    MessageReactions, ReactionCustomEmoji, PeerUser, PeerChannel
)
from datetime import datetime, timedelta
import logging
import base64

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s') 
logging.getLogger('telethon').setLevel(logging.WARNING)

client = None
my_id = None
telegram_loop = None

MEDIA_CACHE_DIR = "media_cache"
STICKER_DB = {}
STICKER_ID_TO_CODENAME = {}
STICKER_JSON_FILE = 'data/stickers.json'

def get_extension_from_mime(mime_type):
    """Возвращает расширение файла на основе MIME-типа."""
    mapping = {
        'image/jpeg': '.jpg',
        'image/png': '.png',
        'image/webp': '.webp',
        'video/mp4': '.mp4',
        'audio/mpeg': '.mp3',
        'audio/ogg': '.ogg',
        'application/pdf': '.pdf',
    }
    return mapping.get(mime_type)

def cleanup_old_cache_files(directory, max_age_days):
    """
    Проверяет указанную директорию и удаляет все файлы,
    которые не изменялись больше чем max_age_days дней.
    """
    logging.info(f"Запуск очистки старых файлов в директории '{directory}' (старше {max_age_days} дней)...")
    try:
        if not os.path.isdir(directory):
            logging.info(f"Директория для очистки '{directory}' не найдена. Пропуск.")
            return

        now = datetime.now()
        max_age = timedelta(days=max_age_days)
        deleted_count = 0

        for filename in os.listdir(directory):
            file_path = os.path.join(directory, filename)
            
            if os.path.isfile(file_path):
                try:
                    file_mod_time_ts = os.path.getmtime(file_path)
                    file_mod_time = datetime.fromtimestamp(file_mod_time_ts)
                    
                    if now - file_mod_time > max_age:
                        os.remove(file_path)
                        logging.info(f"Удален старый файл из кэша: {filename}")
                        deleted_count += 1
                except Exception as e:
                    logging.error(f"Не удалось обработать или удалить файл {file_path}: {e}")

        logging.info(f"Очистка завершена. Удалено файлов: {deleted_count}.")

    except Exception as e:
        logging.error(f"Критическая ошибка во время очистки кэша: {e}", exc_info=True)

async def send_telegram_reaction(chat_id, message_id, emoji):
    """
    Отправляет реакцию на конкретное сообщение.
    Возвращает (bool: success, str: error_message | None).
    """
    if not client or not client.is_connected():
        return False, "Telegram client not connected."

    try:
        logging.info(f"Попытка поставить реакцию '{emoji}' на сообщение {message_id} в чате {chat_id}.")
        
        await client(functions.messages.SendReactionRequest(
            peer=chat_id,
            msg_id=message_id,
            reaction=[ReactionEmoji(emoticon=emoji)]  
        ))

        logging.info(f"Реакция '{emoji}' успешно поставлена.")
        return True, None
    except errors.MessageIdInvalidError:
        logging.warning(f"Не удалось поставить реакцию: сообщение с ID {message_id} не найдено в чате {chat_id}.")
        return True, f"Message ID {message_id} not found (skipped)."
    except errors.ReactionInvalidError:
        logging.error(f"Не удалось поставить реакцию: эмодзи '{emoji}' не является валидной реакцией в этом чате.")
        return True, f"Emoji '{emoji}' is not a valid reaction (skipped)."
    except errors.ChatAdminRequiredError:
         logging.warning(f"Не удалось поставить реакцию в чате {chat_id}: нет прав администратора для отправки реакций.")
         return True, "No permission to send reactions in this chat (skipped)."
    except Exception as e:
        logging.error(f"Неизвестная ошибка при отправке реакции: {e}", exc_info=True)
        return False, f"An unexpected error occurred while sending reaction: {e}"

def load_sticker_db():
    """
    Загружает базу данных стикеров из JSON-файла и создает обратный словарь
    для поиска кодового имени по ID стикера.
    """
    global STICKER_DB, STICKER_ID_TO_CODENAME
    try:
        with open(STICKER_JSON_FILE, 'r', encoding='utf-8') as f:
            STICKER_DB = json.load(f)
        
        temp_mapping = {}
        for codename, data in STICKER_DB.items():
            for sticker_info in data.get("stickers", []):
                temp_mapping[sticker_info['id']] = codename
        
        STICKER_ID_TO_CODENAME = temp_mapping
        logging.info(f"База данных стикеров успешно загружена. Найдено {len(STICKER_DB)} наборов.")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logging.warning(f"Файл `{STICKER_JSON_FILE}` не найден или содержит ошибку: {e}")
        STICKER_DB = {}
        STICKER_ID_TO_CODENAME = {}


load_sticker_db()

async def update_online_status_periodically(client_instance):
    """Фоновая задача для поддержания статуса 'online'."""
    while client_instance and client_instance.is_connected():
        try:
            await client_instance(UpdateStatusRequest(offline=False))
            logging.info("Статус 'online' обновлен.")
        except Exception as e:
            logging.warning(f"Не удалось обновить статус 'online': {e}")
        
        await asyncio.sleep(75)

def make_human_like_typos(text: str,
                    substitution_chance=0.005,
                    transposition_chance=0.005,
                    skip_chance=0.002,
                    lower_chance=0.01) -> str:
    """
    Добавляет в текст человекоподобные опечатки для русского и английского языков,
    cтрого избешая замены букв одного языка на буквы другого.

    Args:
        text: Исходный текст.
        substitution_chance: Вероятность замены буквы на соседнюю по той же ракадке (на символ).
        transposition_chance: Вероятность перестановки двух соседних букв (на символ).
        skip_chance: Вероятность пропуска (удаления) буквы или цифры (на символ).

    Returns:
        Текст с возможными опечатками.
    """

    RU_LOWER = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    RU_UPPER = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
    RU_ALPHABET = set(RU_LOWER + RU_UPPER)

    EN_LOWER = string.ascii_lowercase
    EN_UPPER = string.ascii_uppercase
    EN_ALPHABET = set(EN_LOWER + EN_UPPER)

    RU_TYPO_SUBSTITUTIONS = {
        '.': ['ж', 'ю'],
        '\\': ['ъ'],
        'А': ['К', 'М', 'В', 'П'],
        'Б': ['Л', 'Ь', 'Ю'],
        'В': ['У', 'С', 'Ы', 'А'],
        'Г': ['О', 'Н', 'Ш'],
        'Д': ['Щ', 'Ю', 'Л', 'Ж'],
        'Е': ['П', 'К', 'Н'],
        'Ж': ['З', '.', 'Д', 'Э'],
        'З': ['Ж', 'Щ', 'Х'],
        'И': ['П', 'М', 'Т'],
        'Й': ['Ф', 'Ц'],
        'К': ['А', 'У', 'Е'],
        'Л': ['Ш', 'Б', 'О', 'Д'],
        'М': ['А', 'С', 'И'],
        'Н': ['Р', 'Е', 'Г'],
        'О': ['Г', 'Ь', 'Р', 'Л'],
        'П': ['Е', 'И', 'А', 'Р'],
        'Р': ['Н', 'Т', 'П', 'О'],
        'С': ['В', 'Ч', 'М'],
        'Т': ['Р', 'И', 'Ь'],
        'У': ['В', 'Ц', 'К'],
        'Ф': ['Й', 'Я', 'Ы'],
        'Х': ['Э', 'З', 'Ъ'],
        'Ц': ['Ы', 'Й', 'У'],
        'Ч': ['Ы', 'Я', 'С'],
        'Ш': ['Л', 'Г', 'Щ'],
        'Щ': ['Д', 'Ш', 'З'],
        'Ъ': ['Х', '\\'],
        'Ы': ['Ц', 'Ч', 'Ф', 'В'],
        'Ь': ['О', 'Т', 'Б'],
        'Э': ['Х', 'Ж'],
        'Ю': ['Д', 'Б', '.'],
        'Я': ['Ф', 'Ч'],
        'а': ['к', 'м', 'в', 'п'],
        'б': ['л', 'ь', 'ю'],
        'в': ['у', 'с', 'ы', 'а'],
        'г': ['о', 'н', 'ш'],
        'д': ['щ', 'ю', 'л', 'ж'],
        'е': ['п', 'к', 'н'],
        'ж': ['з', '.', 'д', 'э'],
        'з': ['ж', 'щ', 'х'],
        'и': ['п', 'м', 'т'],
        'й': ['ф', 'ц'],
        'к': ['а', 'у', 'е'],
        'л': ['ш', 'б', 'о', 'д'],
        'м': ['а', 'с', 'и'],
        'н': ['р', 'е', 'г'],
        'о': ['г', 'ь', 'р', 'л'],
        'п': ['е', 'и', 'а', 'р'],
        'р': ['н', 'т', 'п', 'о'],
        'с': ['в', 'ч', 'м'],
        'т': ['р', 'и', 'ь'],
        'у': ['в', 'ц', 'к'],
        'ф': ['й', 'я', 'ы'],
        'х': ['э', 'з', 'ъ'],
        'ц': ['ы', 'й', 'у'],
        'ч': ['ы', 'я', 'с'],
        'ш': ['л', 'г', 'щ'],
        'щ': ['д', 'ш', 'з'],
        'ъ': ['х', '\\'],
        'ы': ['ц', 'ч', 'ф', 'в'],
        'ь': ['о', 'т', 'б'],
        'э': ['х', 'ж'],
        'ю': ['д', 'б', '.'],
        'я': ['ф', 'ч']
    }

    EN_TYPO_SUBSTITUTIONS = {
        'A': ['Q', 'Z', 'S'],
        'B': ['G', 'V', 'N'],
        'C': ['D', 'X', 'V'],
        'D': ['E', 'C', 'S', 'F'],
        'E': ['D', 'W', 'R'],
        'F': ['R', 'V', 'D', 'G'],
        'G': ['T', 'B', 'F', 'H'],
        'H': ['Y', 'N', 'G', 'J'],
        'I': ['K', 'U', 'O'],
        'J': ['U', 'M', 'H', 'K'],
        'K': ['I', ',', 'J', 'L'],
        'L': ['O', '.', 'K', ';'],
        'M': ['J', 'N', ','],
        'N': ['H', 'B', 'M'],
        'O': ['L', 'I', 'P'],
        'P': [';', 'O', '['],
        'Q': ['A', 'W'],
        'R': ['F', 'E', 'T'],
        'S': ['W', 'X', 'A', 'D'],
        'T': ['G', 'R', 'Y'],
        'U': ['J', 'Y', 'I'],
        'V': ['F', 'C', 'B'],
        'W': ['S', 'Q', 'E'],
        'X': ['S', 'Z', 'C'],
        'Y': ['H', 'T', 'U'],
        'Z': ['A', 'X'],
        'a': ['q', 'z', 's'],
        'b': ['g', 'v', 'n'],
        'c': ['d', 'x', 'v'],
        'd': ['e', 'c', 's', 'f'],
        'e': ['d', 'w', 'r'],
        'f': ['r', 'v', 'd', 'g'],
        'g': ['t', 'b', 'f', 'h'],
        'h': ['y', 'n', 'g', 'j'],
        'i': ['k', 'u', 'o'],
        'j': ['u', 'm', 'h', 'k'],
        'k': ['i', ',', 'j', 'l'],
        'l': ['o', '.', 'k', ';'],
        'm': ['j', 'n', ','],
        'n': ['h', 'b', 'm'],
        'o': ['l', 'i', 'p'],
        'p': [';', 'o', '['],
        'q': ['a', 'w'],
        'r': ['f', 'e', 't'],
        's': ['w', 'x', 'a', 'd'],
        't': ['g', 'r', 'y'],
        'u': ['j', 'y', 'i'],
        'v': ['f', 'c', 'b'],
        'w': ['s', 'q', 'e'],
        'x': ['s', 'z', 'c'],
        'y': ['h', 't', 'u'],
        'z': ['a', 'x']
    }

    RU_TYPO_TRANSPOSITIONS = [
        "ст", "тс", "ол", "ло", "ть", "ьт", "но", "он", "ер", "ре",
        "ов", "во", "пр", "рп", "на", "ан", "ко", "ок", "то", "от", "ет", "те",
        "СТ", "ТС", "ОЛ", "ЛО", "ТЬ", "ЬТ", "НО", "ОН", "ЕР", "РЕ",
        "ОВ", "ВО", "ПР", "РП", "НА", "АН", "КО", "ОК", "ТО", "ОТ", "ЕТ", "ТЕ"
    ]

    EN_TYPO_TRANSPOSITIONS = [
        "on", "no", "re", "er", "th", "ht", "in", "ni", "at", "ta",
        "en", "ne", "es", "se", "ou", "uo", "is", "si", "of", "fo", "he", "eh",
        "ON", "NO", "RE", "ER", "TH", "HT", "IN", "NI", "AT", "TA",
        "EN", "NE", "ES", "SE", "OU", "UO", "IS", "SI", "OF", "FO", "HE", "EH"
    ]

    ALL_TYPO_TRANSPOSITIONS_SET = set(RU_TYPO_TRANSPOSITIONS + EN_TYPO_TRANSPOSITIONS)


    chars = list(text)
    new_chars = []
    i = 0
    while i < len(chars):
        char = chars[i]
        processed = False
        
        if char.isupper() and random.random() < lower_chance:
            j = len(new_chars) - 1
            while j >= 0 and new_chars[j].isspace():
                j -= 1
            
            if j >= 0 and new_chars[j] == '.':
                char = char.lower()

        
        if i + 1 < len(chars):
            next_char = chars[i+1]
            current_pair = char + next_char
            
            is_letter_pair = char.isalpha() and next_char.isalpha()
            same_language = (char in RU_ALPHABET and next_char in RU_ALPHABET) or \
                            (char in EN_ALPHABET and next_char in EN_ALPHABET)

          
            should_check_transposition = (current_pair in ALL_TYPO_TRANSPOSITIONS_SET) or \
                                         (is_letter_pair and same_language) or \
                                         (char.isdigit() and next_char.isdigit()) 

            if should_check_transposition and random.random() < transposition_chance:
                new_chars.append(next_char)
                new_chars.append(char)
                i += 2 
                processed = True

        if not processed:

            if char.isalnum() and random.random() < skip_chance:
                i += 1
                processed = True 

            
            elif random.random() < substitution_chance:
                substituted = False
                
                if char in RU_ALPHABET and char in RU_TYPO_SUBSTITUTIONS:
                    possible_typos = RU_TYPO_SUBSTITUTIONS[char]
                    if possible_typos: 
                        typo_char = random.choice(possible_typos)
                        new_chars.append(typo_char)
                        substituted = True
                
                elif char in EN_ALPHABET and char in EN_TYPO_SUBSTITUTIONS:
                     possible_typos = EN_TYPO_SUBSTITUTIONS[char]
                     if possible_typos:
                        typo_char = random.choice(possible_typos)
                        new_chars.append(typo_char)
                        substituted = True
             
                elif (not char.isalnum()) and char in RU_TYPO_SUBSTITUTIONS:
                    possible_typos = RU_TYPO_SUBSTITUTIONS[char]
                    if possible_typos:
                        typo_char = random.choice(possible_typos)
                        new_chars.append(typo_char)
                        substituted = True
                 
                elif (not char.isalnum()) and char in EN_TYPO_SUBSTITUTIONS:
                    possible_typos = EN_TYPO_SUBSTITUTIONS[char]
                    if possible_typos:
                        typo_char = random.choice(possible_typos)
                        new_chars.append(typo_char)
                        substituted = True

                
                if substituted:
                    i += 1
                    processed = True

        
        if not processed:
            new_chars.append(char)
            i += 1

    return "".join(new_chars)

EMOJI_PATTERN = re.compile(
    "({})".format(
        "|".join(re.escape(e) for e in sorted(emoji.EMOJI_DATA.keys(), key=len, reverse=True))
    )
)

def final_fine_tune_sms(comment: str,                     
                    substitution_chance=0.005,
                    transposition_chance=0.005,
                    skip_chance=0.002,
                    lower_chance=0.01) -> str:
    if not comment:
        return "" 

    cleaned_comment = comment.replace('&quot;', '"')

    cleaned_comment = cleaned_comment.replace('—', '-')

    nickname_tag_pattern = r"<\s*ник\s*:.*?>"
    cleaned_comment = re.sub(nickname_tag_pattern, '', cleaned_comment, flags=re.IGNORECASE)

    timestamp_pattern = r'^\[\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}\]\s*'
    cleaned_comment = re.sub(timestamp_pattern, '', cleaned_comment, flags=re.MULTILINE)

    spurious_id_pattern = r'\s*\(\s*(U?ID)\s*:\s*\d+\s*\)\s*'
    cleaned_comment = re.sub(spurious_id_pattern, ' ', cleaned_comment, flags=re.IGNORECASE)

    cleaned_comment = re.sub(r'(.)\1{45,}', lambda m: m.group(1) * 25, cleaned_comment)

    cleaned_comment = re.sub(
        f'{EMOJI_PATTERN.pattern}\\1{{5,}}', 
        lambda m: m.group(1) * 5, 
        cleaned_comment
    )

    def truncate_emoji_sequence(match):
        long_sequence = match.group(0)
        emojis_in_sequence = EMOJI_PATTERN.findall(long_sequence)
        return ''.join(emojis_in_sequence[:14])

    cleaned_comment = re.sub(
        f'(?:{EMOJI_PATTERN.pattern}){{15,}}', 
        truncate_emoji_sequence, 
        cleaned_comment
    )
    
    
    n = len(cleaned_comment)
    last_significant_char_index = -1

    for i in range(n - 1, -1, -1):
        char = cleaned_comment[i]
        if not char.isspace() and not emoji.is_emoji(char):
            last_significant_char_index = i
            break 

    if last_significant_char_index != -1 and cleaned_comment[last_significant_char_index] == '.':
        if last_significant_char_index == 0 or cleaned_comment[last_significant_char_index - 1] != '.':
            cleaned_comment = cleaned_comment[:last_significant_char_index] + cleaned_comment[last_significant_char_index + 1:]

    return make_human_like_typos(cleaned_comment, substitution_chance, transposition_chance, skip_chance, lower_chance)

async def connect_telegram(api_id, api_hash, session_name='telegram_session'):
    """Подключается к Telegram (выполняется в цикле telegram_loop)."""
    global client, my_id
    if client and client.is_connected() and await client.is_user_authorized():
        logging.info("Уже подключены к Telegram.")
        if my_id is None:
            try:
                me = await client.get_me()
                if me: my_id = me.id
            except Exception as e:
                 logging.error(f"Не удалось получить ID пользователя при проверке: {e}")
        return client

    logging.info("Подключение к Telegram в выделенном цикле...")
    client = TelegramClient(session_name, api_id, api_hash,
                            loop=telegram_loop, 
                            system_version="4.16.30-vxCUSTOM")

    try:
        await client.connect()
        if not await client.is_user_authorized():
            logging.info("Требуется авторизация (в консоли, где запущен поток)...")
            phone_number = input("Введите ваш номер телефона (+...): ")
            await client.send_code_request(phone_number)
            try:
                code = input("Введите код из Telegram: ")
                await client.sign_in(phone_number, code)
            except errors.SessionPasswordNeededError:
                password = input("Введите пароль 2FA: ")
                await client.sign_in(password=password)
            logging.info("Авторизация прошла успешно.")
        else:
            logging.info("Авторизация уже пройдена.")

        me = await client.get_me()
        if me:
            my_id = me.id
            logging.info(f"Подключены как {me.first_name} (ID: {my_id}).")
        else:
             logging.error("Не удалось получить информацию о себе после подключения.")
             await client.disconnect()
             return None
        return client
    except errors.PhoneNumberInvalidError:
        logging.error("Неверный формат номера телефона.")
        return None
    except errors.FloodWaitError as e:
         logging.error(f"Слишком много запросов при подключении. Подождите {e.seconds} секунд.")
         return None
    except Exception as e:
        logging.error(f"Ошибка подключения к Telegram: {e}")
        if client and client.is_connected():
             await client.disconnect()
        return None

async def get_chats(limit=50):
    """Получает список последних чатов (личных и групп), отсеивая каналы."""
    if not client or not client.is_connected() or not await client.is_user_authorized():
        logging.warning("get_chats: Клиент Telegram не подключен или не авторизован.")
        return [], "Telegram client not connected or authorized."
    chats = []
    error = None
    try:
        dialogs = await client.get_dialogs(limit=limit)
        for dialog in dialogs:
            entity = dialog.entity
            if hasattr(entity, 'broadcast') and entity.broadcast:
                logging.debug(f"Пропущен канал: {dialog.name} (ID: {dialog.id})")
                continue

            if dialog.is_user or dialog.is_group:

                chat_info = {
                    'id': dialog.id,
                    'name': dialog.name or f"Chat ID: {dialog.id}"
                }
                chats.append(chat_info)

        logging.info(f"Получено {len(chats)} чатов (личные и группы).")
    except errors.AuthKeyError:
        logging.error("Ошибка ключа авторизации при получении чатов. Возможно, сессия повреждена.")
        error = "Authorization key error. Please try restarting the application or deleting the session file."
    except Exception as e:
        logging.error(f"Ошибка получения списка чатов: {e}")
        error = f"Error getting chat list: {e}"
    return chats, error


async def get_chat_info(chat_id):
    """Получает информацию о конкретном чате по ID."""
    if not client or not client.is_connected() or not await client.is_user_authorized():
        logging.warning(f"get_chat_info({chat_id}): Клиент Telegram не подключен или не авторизован.")
        return None, "Telegram client not connected or authorized."
    chat_info = None
    error = None
    try:
        entity = await client.get_entity(chat_id)
        name = getattr(entity, 'title', None) 
        if not name: 
            name = getattr(entity, 'first_name', '')
            last_name = getattr(entity, 'last_name', '')
            if last_name:
                name = f"{name} {last_name}".strip()
        if not name: 
            name = f"ID: {entity.id}"

        chat_info = {'id': entity.id, 'name': name}
    except ValueError:
         logging.error(f"Не удалось найти чат с ID: {chat_id}")
         error = f"Could not find chat with ID: {chat_id}"
    except errors.AuthKeyError:
        logging.error(f"Ошибка ключа авторизации при получении информации о чате {chat_id}.")
        error = "Authorization key error. Please try restarting the application or deleting the session file."
    except Exception as e:
        logging.error(f"Ошибка получения информации о чате {chat_id}: {e}")
        error = f"Error getting chat info for {chat_id}: {e}"
    return chat_info, error

async def get_media_for_message(chat_id, message_id):
    """
    Загружает медиа-контент для ОДНОГО конкретного сообщения.
    Использует дисковый кэш, сохраняя файлы как изображения/видео, а не JSON.
    Возвращает список 'parts' в формате base64, как и раньше, для совместимости с Gemini.
    """
    if not client or not client.is_connected():
        return None, "Telegram client is not connected."
    
    try:
        msg = await client.get_messages(chat_id, ids=message_id)
        if not msg or not msg.media:
            return None, "Message not found or has no media."

        media_type = None
        mime_type = None
        file_ext = None
        
        if isinstance(msg.media, MessageMediaPhoto):
            media_type = 'image'
            mime_type = 'image/jpeg'
            file_ext = '.jpg'
        elif isinstance(msg.media, MessageMediaDocument):
            doc = msg.media.document
            mime_type = getattr(doc, 'mime_type', '')
            file_ext = get_extension_from_mime(mime_type)
            if file_ext == '.mp4': media_type = 'video'
            elif file_ext in ['.mp3', '.ogg']: media_type = 'audio'
            elif file_ext == '.pdf': media_type = 'file'

        if not media_type or not mime_type or not file_ext:
            return None, "Unsupported media type for download."

        cache_filename = f"{chat_id}_{msg.id}{file_ext}"
        cache_filepath = os.path.join(MEDIA_CACHE_DIR, cache_filename)

        if os.path.exists(cache_filepath):
            logging.info(f"Медиа найдено в кэше: {cache_filepath}. Чтение файла...")
            try:
                with open(cache_filepath, 'rb') as f:
                    media_bytes = f.read()
                
                base64_data = base64.b64encode(media_bytes).decode('utf-8')
                part_key = f"{media_type}_base64"
                return [{"mime_type": mime_type, part_key: base64_data}], None

            except Exception as e:
                logging.warning(f"Не удалось прочитать файл из кэша {cache_filepath}, будет произведена повторная загрузка: {e}")

        logging.info(f"Медиа не найдено в кэше. Загрузка из Telegram...")
        media_bytes = await msg.download_media(file=bytes)

        if not media_bytes:
             return None, "Failed to download media from Telegram."

        try:
            os.makedirs(MEDIA_CACHE_DIR, exist_ok=True)
            with open(cache_filepath, 'wb') as f:
                f.write(media_bytes)
            logging.info(f"Медиа сохранено в кэш: {cache_filepath}")
        except IOError as e:
            logging.error(f"Не удалось сохранить медиа в кэш {cache_filepath}: {e}")

        base64_data = base64.b64encode(media_bytes).decode('utf-8')
        part_key = f"{media_type}_base64"
        
        return [{"mime_type": mime_type, part_key: base64_data}], None

    except Exception as e:
        logging.error(f"Ошибка при получении медиа для сообщения {message_id} в чате {chat_id}: {e}", exc_info=True)
        return None, f"An unexpected error occurred: {e}"

async def get_formatted_history(chat_id, limit=60, group_threshold_minutes=4.5, settings=None, download_media=True):
    """
    Получает историю сообщений, форматирует ее по особому, чтобы нейросеть
    могла лучше понимать команды и общение, и объединяет последовательные сообщения.
    """
    global my_id
    if not client or not client.is_connected() or not await client.is_user_authorized():
        return [], "Telegram client not connected or authorized."

    final_formatted_messages = []
    error_message = None

    if my_id is None:
        try:
            me = await client.get_me()
            if me: my_id = me.id
            else: return [], "Error: Could not determine user ID."
        except Exception as e:
            return [], f"Error getting user ID: {e}"

    if settings is None:
        settings = {}

    replace_placeholder_with_empty = settings.get('ignore_all_media', False)
    loading_error_suffix = " - не удалось загрузить."
    group_delta = timedelta(minutes=group_threshold_minutes)
    split_separator = "\n{split}\n"
    is_group_chat = chat_id < 0

    try:
        logging.info(f"Запрос истории для чата {chat_id}, лимит {limit}, режим загрузки: {download_media}")
        messages = await client.get_messages(chat_id, limit=limit)
        logging.info(f"Получено {len(messages)} сообщений.")

        if not messages:
            return [], None

        try:
            await client.send_read_acknowledge(chat_id, max_id=messages[0].id)
        except Exception as read_err:
            logging.warning(f"Не удалось отметить сообщения как прочитанные: {read_err}")


        all_reactions_on_messages = {}
        for msg in messages:
            if msg and isinstance(msg.reactions, MessageReactions) and msg.reactions.recent_reactions:
                reactions_list = []
                for recent_reaction in msg.reactions.recent_reactions:
                    reactor_id = None
                    peer = recent_reaction.peer_id
                    if isinstance(peer, PeerUser):
                        reactor_id = peer.user_id
                    elif isinstance(peer, PeerChannel):
                        reactor_id = peer.channel_id
                    
                    if reactor_id and hasattr(recent_reaction, 'reaction'):
                        emoji = ''
                        if isinstance(recent_reaction.reaction, ReactionEmoji):
                            emoji = recent_reaction.reaction.emoticon
                        if emoji:
                            reactions_list.append({'reactor_id': reactor_id, 'emoji': emoji})
                if reactions_list:
                    all_reactions_on_messages[msg.id] = reactions_list

        raw_intermediate_list = []
        messages.reverse()

        for msg in messages:
            if isinstance(msg, MessageService) or not msg.sender_id:
                continue

            role = "model" if msg.sender_id == my_id else "user"
            timestamp_str = msg.date.strftime("%Y-%m-%d %H:%M:%S")
            id_prefix = f"(ID: {msg.id}) " if role == "user" or is_group_chat else ""
            timestamp_info = f"{id_prefix}\n[{timestamp_str}]"
            sender_prefix = ""
            if is_group_chat and role == "user":
                sender = msg.sender
                sender_name = getattr(sender, 'first_name', '')
                last_name = getattr(sender, 'last_name', '')
                if last_name: sender_name = f"{sender_name} {last_name}".strip()
                if not sender_name: sender_name = getattr(sender, 'username', f"User_{sender.id}") or f"User_{sender.id}"
                sender_prefix = f"<ник:{sender_name.strip()}> "
            
            
            reply_prefix = f"answer({msg.reply_to_msg_id})\n" if msg.reply_to_msg_id else ""
            
            is_media_message = False
            content_parts = []
            content_text = ""

            if msg.media:
                media_processed = False
                if isinstance(msg.media, MessageMediaPhoto):
                    placeholder = "[Изображение]" + loading_error_suffix
                    if settings.get('can_see_photos', True):
                        is_media_message = True
                        if download_media:
                            media_data, _ = await get_media_for_message(chat_id, msg.id)
                            if media_data: content_parts.extend(media_data)
                            else: is_media_message = False; content_text = placeholder
                        else:
                            content_parts.append({"type": "media_placeholder", "chat_id": chat_id, "message_id": msg.id})
                    else:
                        content_text = "" if replace_placeholder_with_empty else placeholder
                    media_processed = True
                elif isinstance(msg.media, MessageMediaDocument):
                    doc = msg.media.document; doc_attrs = getattr(doc, 'attributes', []); doc_mime = getattr(doc, 'mime_type', '')
                    is_video = any(isinstance(attr, DocumentAttributeVideo) for attr in doc_attrs)
                    is_audio = any(isinstance(attr, DocumentAttributeAudio) for attr in doc_attrs)
                    is_pdf = doc_mime == 'application/pdf'
                    if is_video:
                        is_round = any(getattr(attr, 'round_message', False) for attr in doc_attrs)
                        placeholder = ("[Видео-кружок]" if is_round else "[Видео]") + loading_error_suffix
                        if settings.get('can_see_videos', True):
                            is_media_message = True
                            if download_media:
                                media_data, _ = await get_media_for_message(chat_id, msg.id)
                                if media_data: content_parts.extend(media_data)
                                else: is_media_message = False; content_text = placeholder
                            else:
                                content_parts.append({"type": "media_placeholder", "chat_id": chat_id, "message_id": msg.id})
                        else: content_text = "" if replace_placeholder_with_empty else placeholder
                        media_processed = True
                    elif is_audio:
                        is_voice = any(getattr(attr, 'voice', False) for attr in doc_attrs)
                        placeholder = ("[Голосовое сообщение]" if is_voice else "[Аудиофайл]") + loading_error_suffix
                        if settings.get('can_see_audio', True):
                            is_media_message = True
                            if download_media:
                                media_data, _ = await get_media_for_message(chat_id, msg.id)
                                if media_data: content_parts.extend(media_data)
                                else: is_media_message = False; content_text = placeholder
                            else:
                                content_parts.append({"type": "media_placeholder", "chat_id": chat_id, "message_id": msg.id})
                        else: content_text = "" if replace_placeholder_with_empty else placeholder
                        media_processed = True
                    elif is_pdf:
                        placeholder = "[PDF-файл]" + loading_error_suffix
                        if settings.get('can_see_files_pdf', True):
                            is_media_message = True
                            if download_media:
                                media_data, _ = await get_media_for_message(chat_id, msg.id)
                                if media_data: content_parts.extend(media_data)
                                else: is_media_message = False; content_text = placeholder
                            else:
                                content_parts.append({"type": "media_placeholder", "chat_id": chat_id, "message_id": msg.id})
                        else: content_text = "" if replace_placeholder_with_empty else placeholder
                        media_processed = True
                if not media_processed:
                    placeholder_text = "[Медиа]"
                    if isinstance(msg.media, MessageMediaContact): placeholder_text = "[Контакт]"
                    elif isinstance(msg.media, MessageMediaGeo): placeholder_text = "[Геопозиция]"
                    elif isinstance(msg.media, MessageMediaPoll): placeholder_text = "[Опрос]"
                    elif isinstance(msg.media, MessageMediaVenue): placeholder_text = "[Место]"
                    elif isinstance(msg.media, MessageMediaGame): placeholder_text = "[Игра]"
                    elif isinstance(msg.media, MessageMediaInvoice): placeholder_text = "[Счет]"
                    elif isinstance(msg.media, MessageMediaUnsupported): placeholder_text = "[Неподдерживаемое сообщение]"
                    else: placeholder_text = "[Документ]"
                    content_text = "" if replace_placeholder_with_empty else (placeholder_text + loading_error_suffix)
            if msg.text:
                if content_text:
                    content_text = f"{msg.text}\n{content_text}"
                else:
                    content_text = msg.text
            elif msg.sticker and not content_parts:
                codename = STICKER_ID_TO_CODENAME.get(msg.sticker.id, '')
                content_text = f"sticker({codename})" if codename else f"[Стикер]"

            full_text_block = f"{reply_prefix}{timestamp_info}\n{sender_prefix}{content_text}".strip()
            if full_text_block:
                content_parts.insert(0, {"text": full_text_block})

            raw_intermediate_list.append({
                "role": role, "parts": content_parts, "is_media": is_media_message,
                "_original_msg": msg, "_sender_id": msg.sender_id, "_msg_id": msg.id,
                "_reactions_to_prepend": []
            })
        
        msg_id_to_index = {item['_msg_id']: i for i, item in enumerate(raw_intermediate_list)}
        for reacted_on_msg_id, reactions in all_reactions_on_messages.items():
            if reacted_on_msg_id not in msg_id_to_index:
                continue

            start_index = msg_id_to_index[reacted_on_msg_id]
            for reaction in reactions:
                reactor_id = reaction['reactor_id']
                reaction_command = f"react({reacted_on_msg_id})[{reaction['emoji']}]"
                for i in range(start_index + 1, len(raw_intermediate_list)):
                    if raw_intermediate_list[i]['_sender_id'] == reactor_id:
                        if reaction_command not in raw_intermediate_list[i]['_reactions_to_prepend']:
                            raw_intermediate_list[i]['_reactions_to_prepend'].append(reaction_command)
                        break
        
        for item_data in raw_intermediate_list:
            if item_data['_reactions_to_prepend']:
                reactions_text = "\n".join(item_data['_reactions_to_prepend'])
                found_text_part = False
                for part in item_data['parts']:
                    if 'text' in part:
                        part['text'] = f"{reactions_text}\n{part['text']}"
                        found_text_part = True
                        break
                if not found_text_part:
                    item_data['parts'].insert(0, {'text': reactions_text})

        if not raw_intermediate_list:
            return [], None
        for item_data in raw_intermediate_list:
            current_msg_obj, current_parts, current_role, current_is_media = item_data["_original_msg"], item_data["parts"], item_data["role"], item_data["is_media"]
            can_group = False
            if final_formatted_messages:
                last_entry = final_formatted_messages[-1]
                last_msg_obj, last_is_media = last_entry.get("_original_msg"), last_entry.get("is_media", False)
                
                has_reaction_prefix = any('react(' in p.get('text', '') for p in current_parts if 'text' in p)
                if (last_msg_obj and not current_is_media and not last_is_media and not has_reaction_prefix and
                    current_role == last_entry["role"] and current_msg_obj.sender_id == last_msg_obj.sender_id and
                    (current_msg_obj.date - last_msg_obj.date) < group_delta):
                    can_group = True
            if can_group:
                text_to_append = "\n".join([p['text'] for p in current_parts if 'text' in p])
                for part in last_entry["parts"]:
                    if 'text' in part:
                        text_to_append_cleaned = re.sub(r"^\(ID: \d+\)\s*\n\[\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}\]\n(<ник:.*?>\s)?", "", text_to_append)
                        part["text"] += f"{split_separator}{text_to_append_cleaned}"
                        break
                last_entry["_original_msg"] = current_msg_obj
            else:
                final_formatted_messages.append({"role": current_role, "parts": current_parts, "is_media": current_is_media, "_original_msg": current_msg_obj})
        
        for entry in final_formatted_messages:
            entry.pop("_original_msg", None)
            entry.pop("is_media", None)
            if '_sender_id' in entry: del entry['_sender_id']
            if '_msg_id' in entry: del entry['_msg_id']
            if '_reactions_to_prepend' in entry: del entry['_reactions_to_prepend']


        logging.info(f"Успешно отформатировано и сгруппировано {len(final_formatted_messages)} блоков.")
        error_message = None

    except ValueError as e:
        error_message = f"Error: Chat ID {chat_id} not found or invalid."
    except errors.FloodWaitError as e:
         error_message = f"Error: Too many requests to Telegram (history). Wait {e.seconds} sec."
    except errors.AuthKeyError:
        error_message = "Authorization key error. Please try restarting the application or deleting the session file."
    except Exception as e:
        logging.exception(f"Неизвестная ошибка получения истории чата {chat_id}: {e}")
        error_message = f"Unknown error getting chat history: {e}"

    return final_formatted_messages, error_message

async def send_sticker_by_codename(chat_id, codename, settings=None):
    """
    Отправляет случайный стикер из набора по кодовому имени, симулируя выбор.
    Возвращает (bool: success, str: error_message | None).
    """
    if not client or not client.is_connected():
        return False, "Telegram client not connected."

    codename_lower = codename.strip().lower()
    
    if codename_lower not in STICKER_DB:
        logging.warning(f"Стикер с именем '{codename_lower}' не найден в базе. Пропуск.")
        return True, f"Sticker set '{codename_lower}' not found (skipped)."

    sticker_set = STICKER_DB[codename_lower]

    if not sticker_set.get("enabled", False):
        logging.info(f"Набор стикеров '{codename_lower}' отключен. Пропуск.")
        return True, f"Sticker set '{codename_lower}' is disabled (skipped)."
    
    sticker_list = sticker_set.get("stickers", [])
    if not sticker_list:
        logging.warning(f"Набор стикеров '{codename_lower}' пуст. Пропуск.")
        return True, f"Sticker set '{codename_lower}' is empty (skipped)."

    try:
        if settings and isinstance(settings, dict):
            min_delay = settings.get('sticker_choosing_delay_min', 2.0)
            max_delay = settings.get('sticker_choosing_delay_max', 5.5)
        else:
            min_delay = 2.0
            max_delay = 5.5
        
        choosing_delay = random.uniform(min_delay, max_delay)
        logging.info(f"Симуляция выбора стикера в чате {chat_id} на ~{choosing_delay:.2f} сек...")

        async with client.action(chat_id, SendMessageChooseStickerAction()):
            await asyncio.sleep(choosing_delay)
        
        sticker_data = random.choice(sticker_list)
        sticker_to_send = InputDocument(
            id=sticker_data['id'],
            access_hash=sticker_data['access_hash'],
            file_reference=b''
        )
        await client.send_file(chat_id, file=sticker_to_send)
        logging.info(f"Случайный стикер из набора '{codename_lower}' успешно отправлен в чат {chat_id}.")
        return True, None
        
    except Exception as e:
        logging.error(f"Критическая ошибка при отправке стикера из набора '{codename_lower}': {e}", exc_info=True)
        return False, f"API error while sending sticker: {e}"

async def send_telegram_message(chat_id, message_text, settings=None):
    """
    Отправляет сообщение в Telegram, обрабатывая префикс 'answer()' для ответа
    и симулируя печать перед отправкой. Гарантирует удаление ВСЕХ префиксов 'answer()',
    но использует ID для ответа только из ПЕРВОГО найденного.
    """

    if not client or not client.is_connected() or not await client.is_user_authorized():
        logging.warning(f"send_telegram_message({chat_id}): Клиент Telegram не подключен или не авторизован.")
        return False, "Telegram client not connected or authorized."

    reply_to_id = None

    first_match = re.search(r"answer\s*\((\d+)\)", message_text, re.IGNORECASE)

    if first_match:
        id_to_reply_str = first_match.group(1)
        logging.info(f"Найдена инструкция для ответа: '{first_match.group(0)}'. Используется ID: {id_to_reply_str}.")
        try:
            reply_to_id = int(id_to_reply_str)
        except ValueError:
            logging.warning(f"Не удалось преобразовать ID '{id_to_reply_str}' в число. Сообщение будет отправлено без ответа.")
            reply_to_id = None

    message_text = re.sub(r"answer\s*\((\d+)\)", '', message_text, flags=re.IGNORECASE).strip()
    
    if first_match:
        logging.info(f"Текст сообщения после удаления всех тегов answer(): '{message_text[:70]}...'")


    if not message_text or not message_text.strip():
         logging.warning(f"Попытка отправить пустое сообщение в чат {chat_id} (возможно, после удаления 'answer()'). Отправка отменена.")
         return True, "Message became empty after removing 'answer()' tag, sending cancelled."
    
    try:
        if settings and isinstance(settings, dict):
            sub_chance = settings.get('substitution_chance', 0.005)
            trans_chance = settings.get('transposition_chance', 0.005)
            skip_chance_val = settings.get('skip_chance', 0.002)
            lower_chance_val = settings.get('lower_chance', 0.05)

            
            message_text = final_fine_tune_sms(
                message_text,
                substitution_chance=sub_chance,
                transposition_chance=trans_chance,
                skip_chance=skip_chance_val,
                lower_chance=lower_chance_val
            )
        else:
            message_text = final_fine_tune_sms(message_text)
    except Exception as e:
        logging.error(f"Ошибка при вызове final_fine_tune_sms: {e}")
    
    if not message_text or not message_text.strip():
        logging.warning(f"Попытка отправить пустое сообщение в чат {chat_id}(вероятно из-за final_fine_tune_sms). Отправка отменена.")
        return True, "Message empty after final_fine_tune_sms, sending cancelled."

    success = False
    error = None

    try:
        if settings and isinstance(settings, dict):
            min_delay_ms = settings.get('typing_delay_ms_min', 40.0)
            max_delay_ms = settings.get('typing_delay_ms_max', 90.0)
            min_think_s = settings.get('base_thinking_delay_s_min', 1.2)
            max_think_s = settings.get('base_thinking_delay_s_max', 2.8)
            max_duration = settings.get('max_typing_duration_s', 25.0)
        else:
            min_delay_ms, max_delay_ms = 40.0, 90.0
            min_think_s, max_think_s = 1.2, 2.8
            max_duration = 25.0

        chars_count = len(message_text)
        typing_delay_per_char_ms = random.uniform(min_delay_ms, max_delay_ms)
        base_thinking_delay_s = random.uniform(min_think_s, max_think_s)
        total_typing_duration_s = (chars_count * typing_delay_per_char_ms) / 1000.0
        
        if max_duration > 0:
            total_typing_duration_s = max(1.5, min(total_typing_duration_s, max_duration))
        else:
            total_typing_duration_s = 0.0
            base_thinking_delay_s = 0.0 
        
        full_delay_s = base_thinking_delay_s + total_typing_duration_s
        
        logging.info(f"Симуляция печати в чате {chat_id} на ~{full_delay_s:.2f} сек...")

        async with client.action(chat_id, 'typing'):
            await asyncio.sleep(full_delay_s)


        logging.info(f"Отправка сообщения в чат {chat_id} (ответ на {reply_to_id if reply_to_id else 'нет'})...")
        await client.send_message(chat_id, message_text, reply_to=reply_to_id)
        logging.info(f"Сообщение успешно отправлено в чат {chat_id}.")
        success = True

    except errors.MsgIdInvalidError as e:
        
        if reply_to_id:
            logging.warning(f"Не удалось ответить на сообщение ID {reply_to_id} в чате {chat_id}: неверный ID сообщения ({e}). Попытка отправить без ответа...")
            error = f"Invalid reply message ID {reply_to_id}. Sending without reply."
            reply_to_id = None
            try:
                
                logging.info(f"Повторная отправка в чат {chat_id} (уже без ответа). Текст: '{message_text[:50].replace(chr(10), ' ')}...'")
                await client.send_message(chat_id, message_text)
                logging.info(f"Сообщение успешно отправлено в чат {chat_id} (без ответа после ошибки MsgIdInvalidError).")
                success = True
                error = None
            except Exception as e_retry:
                logging.exception(f"Ошибка при повторной отправке сообщения (без ответа) в чат {chat_id}: {e_retry}")
                success = False
                error = f"Failed to send message even after reply error ({e}): {e_retry}"
        else:
            logging.exception(f"Неожиданная ошибка MsgIdInvalidError при отправке в чат {chat_id}, хотя reply_to_id был None: {e}")
            success = False
            error = f"Unexpected MsgIdInvalidError without reply attempt: {e}"

    except ValueError as e:
         logging.error(f"Ошибка отправки в чат {chat_id}: Неверный ID чата или чат не найден. ({e})")
         success = False
         error = f"Error: Chat ID '{chat_id}' not found or invalid for sending."
    except errors.FloodWaitError as e:
         logging.error(f"Слишком много запросов при отправке в чат {chat_id}. Подождите {e.seconds} секунд.")
         success = False
         error = f"Error: Too many requests to Telegram (sending). Wait {e.seconds} sec."
    except errors.AuthKeyError as e:
        logging.error(f"Ошибка ключа авторизации при отправке сообщения в чат {chat_id}: {e}")
        success = False
        error = "Authorization key error. Please try restarting the application or deleting the session file."
    except errors.UserIsBlockedError as e:
        logging.error(f"Не удалось отправить сообщение в чат {chat_id}: пользователь заблокировал бота (или вас). {e}")
        success = False
        error = "Message could not be sent: User has blocked the bot (or you)."
    except errors.ChatWriteForbiddenError as e:
        logging.error(f"Не удалось отправить сообщение в чат {chat_id}: нет прав на отправку сообщений. {e}")
        success = False
        error = "Message could not be sent: No permission to write in this chat."
    except Exception as e:
        logging.exception(f"Непредвиденная ошибка отправки сообщения в чат {chat_id}: {e}")
        success = False
        error = f"Unexpected error sending message: {e}"

    return success, error

async def disconnect_telegram():
    """Отключается от Telegram (выполняется в цикле telegram_loop)."""
    global client
    if client and client.is_connected():
        logging.info("Отключение от Telegram в выделенном цикле...")
        await client.disconnect()
        logging.info("Отключено.")
    client = None

async def telegram_main_loop(api_id, api_hash, session_name, ready_event):
    global telegram_loop, client
    telegram_loop = asyncio.get_running_loop()
    logging.info(f"Цикл событий Telethon запущен: {telegram_loop}")

    client = await connect_telegram(api_id, api_hash, session_name)

    if client:
        logging.info(">>> Клиент готов к работе в режиме активных запросов.")
        
        asyncio.create_task(update_online_status_periodically(client))
        logging.info("Фоновая задача для поддержания статуса 'online' запущена.")

        ready_event.set()
        await client.run_until_disconnected()
    else:
        logging.error("Не удалось подключиться к Telegram. Поток завершается.")
        ready_event.set()

    logging.info("Поток Telethon завершает работу.")

def run_in_telegram_loop(coro):
    """
    Выполняет корутину в цикле событий потока Telethon и возвращает результат.
    Блокирует вызывающий поток Flask до получения результата.
    """
    default_error_results = {
        'get_chats': ([], "Telegram event loop not available or not running."),
        'get_chat_info': (None, "Telegram event loop not available or not running."),
        'get_formatted_history': ([], "Telegram event loop not available or not running."),
        'send_telegram_message': (False, "Telegram event loop not available or not running."),
    }
    coro_name = getattr(coro, '__name__', 'unknown')

    if not telegram_loop or not telegram_loop.is_running():
        logging.error(f"{coro_name}: Цикл событий Telethon не запущен или недоступен.")
        return default_error_results.get(coro_name, (None, "Telegram event loop not available or not running."))

    global client
    if coro_name != 'connect_telegram':
        if not client or not client.is_connected():
             logging.warning(f"{coro_name}: Попытка выполнить задачу, когда клиент не подключен.")
             error_msg = "Telegram client is not connected."
             return default_error_results.get(coro_name, (None, error_msg))


    future = asyncio.run_coroutine_threadsafe(coro, telegram_loop)
    try:
        result = future.result(timeout=60)
        return result
    except asyncio.TimeoutError:
         logging.error(f"Операция '{coro_name}' в потоке Telethon заняла слишком много времени (>60s).")
         telegram_loop.call_soon_threadsafe(future.cancel)
         error_msg = f"Telegram operation '{coro_name}' timed out."
         return default_error_results.get(coro_name, (None, error_msg))
    except Exception as e:
        logging.exception(f"Ошибка при выполнении/получении результата '{coro_name}' из потока Telethon: {e}")
        error_msg = f"Error during '{coro_name}' execution in Telegram thread: {e}"
        return default_error_results.get(coro_name, (None, error_msg))
