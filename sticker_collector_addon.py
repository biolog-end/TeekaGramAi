import asyncio
import json
import logging
import re
import os
from dotenv import load_dotenv

from telethon import TelegramClient, events
from telethon.tl.types import InputDocument

load_dotenv()
TELAGRAMM_API_ID = os.getenv('TELAGRAMM_API_ID')
TELAGRAMM_API_HASH = os.getenv('TELAGRAMM_API_HASH')

if not TELAGRAMM_API_ID or not TELAGRAMM_API_HASH:
    raise ValueError("TELAGRAMM_API_ID и TELAGRAMM_API_HASH должны быть установлены в .env файле")

# --- Настройки ---
STICKER_JSON_FILE = 'data/stickers.json'
ACCOUNTS_JSON_FILE = 'data/accounts.json'

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

temp_sticker_data = None
waiting_for_description_for = None
session_message_ids = []

def load_sticker_db():
    try:
        with open(STICKER_JSON_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_sticker_db(data):
    with open(STICKER_JSON_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    logging.info(f"База данных в файле '{STICKER_JSON_FILE}' обновлена.")

def get_first_account_session():
    """Загружает путь к сессии первого аккаунта из 'accounts.json'."""
    try:
        with open(ACCOUNTS_JSON_FILE, 'r', encoding='utf-8') as f:
            accounts = json.load(f)
            if not accounts:
                raise ValueError(f"Файл '{ACCOUNTS_JSON_FILE}' пуст.")
            
            first_account_path = next(iter(accounts.values()))
            
            session_path, _ = os.path.splitext(first_account_path)
            return session_path
    except (FileNotFoundError, json.JSONDecodeError, StopIteration, ValueError) as e:
        logging.error(f"Ошибка чтения файла аккаунтов '{ACCOUNTS_JSON_FILE}': {e}")
        logging.error("Пожалуйста, убедитесь, что файл 'data/accounts.json' существует, не пуст и содержит корректные данные.")
        return None

async def send_and_track(client, chat_id, *args, **kwargs):
    """Обёртка для отправки сообщений, которая записывает ID отправленного сообщения."""
    global session_message_ids
    sent_message = await client.send_message(chat_id, *args, **kwargs)
    if sent_message:
        session_message_ids.append(sent_message.id)
    return sent_message

async def send_file_and_track(client, chat_id, *args, **kwargs):
    """Обёртка для отправки файлов/стикеров, которая записывает ID."""
    global session_message_ids
    sent_message = await client.send_file(chat_id, *args, **kwargs)
    if sent_message:
        session_message_ids.append(sent_message.id)
    return sent_message


async def main():
    global temp_sticker_data, waiting_for_description_for, session_message_ids
    
    session_to_use = get_first_account_session()
    if not session_to_use:
        return
        
    logging.info(f"Используется сессия: '{session_to_use}'")
    
    client = TelegramClient(session_to_use, TELAGRAMM_API_ID, TELAGRAMM_API_HASH)

    async with client:
        me = await client.get_me()
        if not me:
            logging.error("Не удалось получить информацию о себе. Проверьте, активна ли сессия.")
            return
            
        my_chat_id = me.id
        logging.info(f"Скрипт будет отслеживать сообщения в чате 'Избранное' (ID: {my_chat_id})")

        @client.on(events.NewMessage(chats=my_chat_id))
        async def message_handler(event):
            global temp_sticker_data, waiting_for_description_for, session_message_ids
            
            message = event.message
            session_message_ids.append(message.id)

            if message.sticker:
                if waiting_for_description_for:
                    await send_and_track(client, my_chat_id, f"Ожидание описания для `{waiting_for_description_for}` отменено.", reply_to=message.id)
                    waiting_for_description_for = None
                
                temp_sticker_data = {"id": message.sticker.id, "access_hash": message.sticker.access_hash}
                logging.info(f"Получен стикер (ID: {temp_sticker_data['id']}). Ожидаю кодовое имя...")
                await send_and_track(client, my_chat_id, "Стикер получен. Теперь отправьте его кодовое имя.", reply_to=message.id)
                return

            if message.text:
                text_input = message.text.strip()
                text_input_lower = text_input.lower()
                
                if text_input_lower == 'clear':
                    logging.info(f"Получена команда 'clear'. Будет удалено {len(session_message_ids)} сообщений.")
                    if not session_message_ids:
                        await send_and_track(client, my_chat_id, "Нечего удалять, сессия чиста.", reply_to=message.id)
                        return

                    try:
                        deleted_count = await client.delete_messages(my_chat_id, session_message_ids)
                        logging.info(f"Удалено {len(deleted_count)} сообщений.")
                        
                        confirm_msg = await client.send_message(my_chat_id, f"✅ Очищено {len(deleted_count)} сообщений.")
                        
                        session_message_ids = [confirm_msg.id]
                        
                        await asyncio.sleep(5)
                        await client.delete_messages(my_chat_id, [confirm_msg.id])
                        session_message_ids.remove(confirm_msg.id)

                    except Exception as e:
                        logging.error(f"Ошибка при удалении сообщений: {e}")
                        await send_and_track(client, my_chat_id, f"❌ Ошибка при очистке: {e}", reply_to=message.id)
                    return

                if text_input_lower == 'all':
                    sticker_db = load_sticker_db()
                    if not sticker_db:
                        await send_and_track(client, my_chat_id, "База стикеров пока пуста.", reply_to=message.id)
                        return
                    await send_and_track(client, my_chat_id, "Начинаю показ всех сохраненных стикеров...", reply_to=message.id)
                    for codename, data in sticker_db.items():
                        header = f"--- **{codename}** ---"
                        if data.get("description"):
                            header += f"\n*Описание:* {data['description']}"
                        await send_and_track(client, my_chat_id, header, parse_mode='md')
                        for sticker_data in data.get("stickers", []):
                            sticker_to_send = InputDocument(id=sticker_data['id'], access_hash=sticker_data['access_hash'], file_reference=b'')
                            await send_file_and_track(client, my_chat_id, file=sticker_to_send)
                    await send_and_track(client, my_chat_id, "--- Показ завершен ---")
                    return
                
                set_creation_match = re.match(r"набор\s*\(([\w\d_-]+)\)", text_input_lower)
                if set_creation_match:
                    codename = set_creation_match.group(1)
                    logging.info(f"Получена команда на создание пустого набора '{codename}'.")
                    
                    sticker_db = load_sticker_db()
                    if codename in sticker_db:
                        await send_and_track(client, my_chat_id, f"🟡 Набор `{codename}` уже существует.", reply_to=message.id)
                        return
                    
                    sticker_db[codename] = { "enabled": True, "description": "", "stickers": [] }
                    save_sticker_db(sticker_db)
                    
                    waiting_for_description_for = codename
                    
                    logging.info(f"Пустой набор '{codename}' создан. Ожидание описания...")
                    await send_and_track(client, my_chat_id, f"✅ Пустой набор `{codename}` создан. Теперь отправьте для него текст описания.", reply_to=message.id)
                    return

                description_match = re.match(r"описание\s*\(([\w\d_-]+)\)", text_input_lower)
                if description_match:
                    codename_to_describe = description_match.group(1)
                    sticker_db = load_sticker_db()
                    if codename_to_describe in sticker_db:
                        waiting_for_description_for = codename_to_describe
                        await send_and_track(client, my_chat_id, f"Принято. Теперь отправьте текст описания для `{codename_to_describe}`.", reply_to=message.id)
                    else:
                        await send_and_track(client, my_chat_id, f"Набор с именем `{codename_to_describe}` не найден.", reply_to=message.id)
                    return

                if waiting_for_description_for:
                    codename = waiting_for_description_for
                    sticker_db = load_sticker_db()
                    sticker_db[codename]['description'] = text_input
                    save_sticker_db(sticker_db)
                    await send_and_track(client, my_chat_id, f"✅ Описание для `{codename}` сохранено.", reply_to=message.id)
                    waiting_for_description_for = None
                    return

                if temp_sticker_data:
                    codename = text_input_lower
                    sticker_db = load_sticker_db()
                    confirmation_text = ""
                    if codename in sticker_db:
                        sticker_list = sticker_db[codename].get("stickers", [])
                        if any(s['id'] == temp_sticker_data['id'] for s in sticker_list):
                            await send_and_track(client, my_chat_id, f"🟡 **Дубликат!**", reply_to=message.id)
                            temp_sticker_data = None; return
                        sticker_list.append(temp_sticker_data)
                        sticker_db[codename]["stickers"] = sticker_list
                        confirmation_text = f"✅ **Стикер добавлен!**\n`{codename}`: {len(sticker_list)} шт."
                    else:
                        sticker_db[codename] = {"enabled": True, "description": "", "stickers": [temp_sticker_data]}
                        confirmation_text = f"✅ **Успешно!**\nСоздан набор `{codename}`."
                    
                    save_sticker_db(sticker_db)
                    
                    sticker_to_send = InputDocument(id=temp_sticker_data['id'], access_hash=temp_sticker_data['access_hash'], file_reference=b'')
                    await send_file_and_track(client, my_chat_id, file=sticker_to_send)
                    await send_and_track(client, my_chat_id, confirmation_text, parse_mode='md')
                    temp_sticker_data = None
                    return
        
        print("-" * 50)
        print("Скрипт для сбора стикеров запущен")
        print("\nКоманды:")
        print("1. [Стикер] -> [имя] - Добавить/создать набор.")
        print("2. 'all' - Показать все наборы.")
        print("3. 'описание(имя)' -> [текст] - Добавить описание.")
        print("4. 'набор(имя)' - Создать пустой набор для описания группы.")
        print("5. 'clear' - Удалить все сообщения за сессию.")
        print("\nДля остановки нажмите Ctrl+C.")
        print("-" * 50)
        await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())