# File: sticker_collector.py

import asyncio
import json
import logging
import re

from telethon import TelegramClient, events
from telethon.tl.types import InputDocument

import config as app_config

# --- Настройки ---
SESSION_NAME = 'kadzu'
TARGET_CHAT_ID = 5495213645
STICKER_JSON_FILE = 'stickers.json'

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Глобальные переменные для отслеживания состояния сессии ---
temp_sticker_data = None
waiting_for_description_for = None
# Новый список для хранения ID сообщений, созданных за эту сессию
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
    
    client = TelegramClient(SESSION_NAME, app_config.TELAGRAMM_API_ID, app_config.TELAGRAMM_API_HASH)

    @client.on(events.NewMessage(chats=TARGET_CHAT_ID))
    async def message_handler(event):
        global temp_sticker_data, waiting_for_description_for, session_message_ids
        
        message = event.message
        # Запоминаем ID каждого нового входящего сообщения
        session_message_ids.append(message.id)

        # Сценарий 1: Получили стикер
        if message.sticker:
            if waiting_for_description_for:
                reply = await send_and_track(client, TARGET_CHAT_ID, f"Ожидание описания для `{waiting_for_description_for}` отменено.", reply_to=message.id)
                waiting_for_description_for = None
            
            temp_sticker_data = {"id": message.sticker.id, "access_hash": message.sticker.access_hash}
            logging.info(f"Получен стикер (ID: {temp_sticker_data['id']}). Ожидаю кодовое имя...")
            await send_and_track(client, TARGET_CHAT_ID, "Стикер получен. Теперь отправьте его кодовое имя.", reply_to=message.id)
            return

        # Сценарий 2: Получили текст
        if message.text:
            text_input = message.text.strip()
            text_input_lower = text_input.lower()
            
            # --- Подсценарий 2.1: Команда 'clear' ---
            if text_input_lower == 'clear':
                logging.info(f"Получена команда 'clear'. Будет удалено {len(session_message_ids)} сообщений.")
                if not session_message_ids:
                    await send_and_track(client, TARGET_CHAT_ID, "Нечего удалять, сессия чиста.", reply_to=message.id)
                    return

                try:
                    # Удаляем только для себя (в "Избранном" это равносильно полному удалению)
                    deleted_count = await client.delete_messages(TARGET_CHAT_ID, session_message_ids)
                    logging.info(f"Удалено {len(deleted_count)} сообщений.")
                    
                    # После удаления отправляем временное сообщение и тоже его запоминаем
                    confirm_msg = await client.send_message(TARGET_CHAT_ID, f"✅ Очищено {len(deleted_count)} сообщений.")
                    
                    # Очищаем список и начинаем его заново с ID этого подтверждения
                    session_message_ids = [confirm_msg.id]
                    
                    # Можно настроить автоудаление подтверждения через пару секунд
                    await asyncio.sleep(5)
                    await client.delete_messages(TARGET_CHAT_ID, [confirm_msg.id])
                    session_message_ids.remove(confirm_msg.id)

                except Exception as e:
                    logging.error(f"Ошибка при удалении сообщений: {e}")
                    await send_and_track(client, TARGET_CHAT_ID, f"❌ Ошибка при очистке: {e}", reply_to=message.id)
                return

            # --- Остальные команды (all, описание, и т.д.) ---
            if text_input_lower == 'all':
                sticker_db = load_sticker_db()
                if not sticker_db:
                    await send_and_track(client, TARGET_CHAT_ID, "База стикеров пока пуста.", reply_to=message.id)
                    return
                await send_and_track(client, TARGET_CHAT_ID, "Начинаю показ всех сохраненных стикеров...", reply_to=message.id)
                for codename, data in sticker_db.items():
                    header = f"--- **{codename}** ---"
                    if data.get("description"):
                        header += f"\n*Описание:* {data['description']}"
                    await send_and_track(client, TARGET_CHAT_ID, header, parse_mode='md')
                    for sticker_data in data.get("stickers", []):
                        sticker_to_send = InputDocument(id=sticker_data['id'], access_hash=sticker_data['access_hash'], file_reference=b'')
                        await send_file_and_track(client, TARGET_CHAT_ID, file=sticker_to_send)
                await send_and_track(client, TARGET_CHAT_ID, "--- Показ завершен ---")
                return
            
            set_creation_match = re.match(r"набор\s*\(([\w\d_-]+)\)", text_input_lower)
            if set_creation_match:
                codename = set_creation_match.group(1)
                logging.info(f"Получена команда на создание пустого набора '{codename}'.")
                
                sticker_db = load_sticker_db()
                if codename in sticker_db:
                    await send_and_track(client, TARGET_CHAT_ID, f"🟡 Набор `{codename}` уже существует.", reply_to=message.id)
                    return
                
                # Создаем новый набор с пустым списком стикеров
                sticker_db[codename] = {
                    "enabled": True,
                    "description": "",
                    "stickers": [] # <-- Ключевое отличие: пустой список
                }
                save_sticker_db(sticker_db)
                
                # Сразу переходим в режим ожидания описания для этого нового набора
                waiting_for_description_for = codename
                
                logging.info(f"Пустой набор '{codename}' создан. Ожидание описания...")
                await send_and_track(client, TARGET_CHAT_ID, f"✅ Пустой набор `{codename}` создан. Теперь отправьте для него текст описания.", reply_to=message.id)
                return
            # --- КОНЕЦ НОВОГО БЛОКА ---

            description_match = re.match(r"описание\s*\(([\w\d_-]+)\)", text_input_lower)
            if description_match:
                codename_to_describe = description_match.group(1)
                sticker_db = load_sticker_db()
                if codename_to_describe in sticker_db:
                    waiting_for_description_for = codename_to_describe
                    await send_and_track(client, TARGET_CHAT_ID, f"Принято. Теперь отправьте текст описания для `{codename_to_describe}`.", reply_to=message.id)
                else:
                    await send_and_track(client, TARGET_CHAT_ID, f"Набор с именем `{codename_to_describe}` не найден.", reply_to=message.id)
                return

            if waiting_for_description_for:
                codename = waiting_for_description_for
                sticker_db = load_sticker_db()
                sticker_db[codename]['description'] = text_input
                save_sticker_db(sticker_db)
                await send_and_track(client, TARGET_CHAT_ID, f"✅ Описание для `{codename}` сохранено.", reply_to=message.id)
                waiting_for_description_for = None
                return

            if temp_sticker_data:
                codename = text_input_lower
                sticker_db = load_sticker_db()
                confirmation_text = ""
                if codename in sticker_db:
                    sticker_list = sticker_db[codename].get("stickers", [])
                    if any(s['id'] == temp_sticker_data['id'] for s in sticker_list):
                        await send_and_track(client, TARGET_CHAT_ID, f"🟡 **Дубликат!**", reply_to=message.id)
                        temp_sticker_data = None; return
                    sticker_list.append(temp_sticker_data)
                    sticker_db[codename]["stickers"] = sticker_list
                    confirmation_text = f"✅ **Стикер добавлен!**\n`{codename}`: {len(sticker_list)} шт."
                else:
                    sticker_db[codename] = {"enabled": True, "description": "", "stickers": [temp_sticker_data]}
                    confirmation_text = f"✅ **Успешно!**\nСоздан набор `{codename}`."
                
                save_sticker_db(sticker_db)
                
                sticker_to_send = InputDocument(id=temp_sticker_data['id'], access_hash=temp_sticker_data['access_hash'], file_reference=b'')
                await send_file_and_track(client, TARGET_CHAT_ID, file=sticker_to_send)
                await send_and_track(client, TARGET_CHAT_ID, confirmation_text, parse_mode='md')
                temp_sticker_data = None
                return
    
    # Код запуска
    async with client:
        print("-" * 50)
        print("Скрипт для сбора стикеров запущен (v3.2 - с командой 'набор').")
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